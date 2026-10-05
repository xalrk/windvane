"""`windvane watch`: follow the autopilot's sessions live from their streams in `runs/` (what each says, the
commands it runs and their output) and the driver's log. Local only: costs no tokens."""
import json
import re
import shutil
import sys
import time
import unicodedata

tty = sys.stdout.isatty()
c = lambda code, s: f"\033[{code}m{s}\033[0m" if tty else s


def clip(text, n):
    rows = str(text).rstrip("\n").split("\n")
    return "\n".join(rows[:n]) + (f"\n… (+{len(rows) - n} lines)" if len(rows) > n else "")


# Markdown, rendered the way Claude Code's terminal does: line by line, so every rendered line closes its own
# colours and clipping between lines can never leave one on. Without colour the markers are dropped all the same.
SGR = {"b": "1", "dim": "2", "i": "3", "s": "9", "h": "36", "code": "94"}  # later keys win on a clash
CODE = re.compile(r"(`+)(.+?)\1")
ESC = re.compile(r"\\([\\`*_{}\[\]()#+\-.!|>~])")
HOLD = re.compile("[\ue000-\uefff]")  # placeholders for code spans and escapes, so no marker inside them counts
SPAN = re.compile(
    # a URL's own (…) pair stays in it, one level deep: a(b(c)) ends the URL at the first ")"
    r"\[(?P<lt>[^\]]+)\]\((?P<url>(?:[^()\s]|\([^()\s]*\))+)\)"
    r"|\*\*(?=\S)(?P<b1>.+?)(?<=\S)\*\*"
    r"|(?<!\w)__(?=\S)(?P<b2>.+?)(?<=\S)__(?!\w|\.\w)"
    r"|(?<![\w*])\*(?=[^\s*])(?P<i1>.+?)(?<=[^\s*])\*(?![\w*])"  # not intra-word: 2*3*4 stays as typed
    r"|(?<!\w)_(?=[^\s_])(?P<i2>.+?)(?<=[^\s_])_(?!\w|\.\w)"  # snake_case_names, __init__.py stay
    r"|~~(?=\S)(?P<s>.+?)(?<=\S)~~")
FENCE = re.compile(r"\s*(`{3,}|~{3,})\s*([^\s`]*)")
HEADING = re.compile(r" {0,3}#{1,6}\s+(.*?)(\s+#+)?\s*$")
RULE = re.compile(r" {0,3}([-*_])(\s*\1){2,}\s*$")
BULLET = re.compile(r"(\s*)[-*+]\s+(.*)")
NUMBERED = re.compile(r"(\s*)(\d+[.)])\s+(.*)")
QUOTE = re.compile(r"\s*>\s?(.*)")
TABLE_RULE = re.compile(r"\s*\|?\s*:?-+:?\s*(\|\s*:?-+:?\s*)*\|?\s*$")
TEXT_LINES = 12  # counted lines `watch` shows of one assistant message before it clips


def inline(text, st=frozenset()):
    """Text as (piece, styles) segments. Code spans and backslash escapes are held out first, so `*.py` and
    `\\*` never open emphasis, while **a `b` c** still wraps the code span."""
    held = []

    def hold(literal, code):
        held.append((literal, code))
        return chr(0xE000 + len(held) - 1)
    text = CODE.sub(lambda m: hold(m.group(2).strip() or m.group(2), True), text)
    return spans(ESC.sub(lambda m: hold(m.group(1), False), text), st, held)


def spans(text, st, held):
    out, at = [], 0
    for m in SPAN.finditer(text):
        out += unhold(text[at:m.start()], st, held)
        if m.group("lt"):
            out += spans(m.group("lt"), st, held)
            if m.group("url") != m.group("lt"):
                out.append((f" ({m.group('url')})", st | {"dim"}))
        else:
            key = "b" if m.group("b1") or m.group("b2") else "i" if m.group("i1") or m.group("i2") else "s"
            out += spans(next(g for g in m.group("b1", "b2", "i1", "i2", "s") if g), st | {key}, held)
        at = m.end()
    return out + unhold(text[at:], st, held)


def unhold(text, st, held):
    out, at = [], 0
    for m in HOLD.finditer(text):
        i = ord(m.group()) - 0xE000
        if i < len(held):
            out += [(text[at:m.start()], st), (held[i][0], st | {"code"} if held[i][1] else st)]
            at = m.end()
    return out + [(text[at:], st)]


def paint(segs, color):
    """Segments as one line; with colour, every change of style starts from a reset and the line ends reset."""
    if not color:
        return "".join(t for t, _ in segs)
    out, cur = [], frozenset()
    for t, st in segs:
        if t and st != cur:
            out.append(("\033[0m" if cur else "") + (f"\033[{';'.join(SGR[k] for k in SGR if k in st)}m" if st else ""))
            cur = st
        out.append(t)
    return "".join(out) + ("\033[0m" if cur else "")


def width(s):
    """Columns a string takes in a terminal (wide CJK/emoji count two, combining marks none)."""
    return sum(0 if unicodedata.combining(ch) else 2 if unicodedata.east_asian_width(ch) in "WF" else 1 for ch in s)


def cells(row):
    """A pipe-table row's cells; a `|` inside a code span or escaped as `\\|` doesn't split."""
    s = row.strip()
    s = s[1:] if s.startswith("|") else s
    s = s[:-1] if s.endswith("|") and not s.endswith("\\|") else s
    out, cur, tick, i = [], "", False, 0
    while i < len(s):
        if s[i] == "\\" and s[i + 1:i + 2] == "|":
            cur, i = cur + "\\|", i + 2
            continue
        tick = not tick if s[i] == "`" else tick
        if s[i] == "|" and not tick:
            out.append(cur.strip())
            cur = ""
        else:
            cur += s[i]
        i += 1
    return out + [cur.strip()]


def table(rows, rule, color, cols=0):
    """Rows padded to aligned columns (`:--:` centres, `--:` right-aligns), the header bold, a rule under it, as
    (line, counts) pairs. A table wider than `cols` narrows its widest columns and wraps their cells, the terminal
    would otherwise wrap it to column 0; a rule on each side of a row that wrapped keeps it one row. One that can't
    fit with every column 4 wide becomes records (see `records`)."""
    aligns = [("^" if a.startswith(":") else ">") if a.endswith(":") else "<" for a in cells(rule)]
    grid = [[inline(x, frozenset({"b"}) if r == 0 else frozenset()) for x in cells(row)] for r, row in enumerate(rows)]
    n = max(len(r) for r in grid)
    grid = [r + [[]] * (n - len(r)) for r in grid]
    widths = [max(width("".join(t for t, _ in r[k])) for r in grid) for k in range(n)]
    natural = list(widths)
    while cols and sum(widths) + 3 * (n - 1) > cols and max(widths) > 4:
        widths[widths.index(max(widths))] -= 1
    if cols and sum(widths) + 3 * (n - 1) > cols:
        return records(rows, color, cols)
    aligns += ["<"] * n
    bar = (" │ ", frozenset({"dim"}))
    dim_rule = lambda: (paint([("─┼─".join("─" * w for w in widths), frozenset({"dim"}))], color), False)
    out, tall = [], False
    for r, row in enumerate(grid):
        parts = [wrap(segs, widths[k], least=1) for k, segs in enumerate(row)]
        if r > 1 and (tall or max(len(p) for p in parts) > 1):  # rows of one line each need no rule between them
            out.append(dim_rule())
        tall = max(len(p) for p in parts) > 1
        for j in range(max(len(p) for p in parts)):
            line = []
            for k, p in enumerate(parts):
                segs = p[j] if j < len(p) else []
                gap = widths[k] - width("".join(t for t, _ in segs))
                left = {"<": 0, ">": gap, "^": gap // 2}[aligns[k]]
                pad = [(" " * (gap - left), frozenset())] if k < n - 1 else []
                line += ([bar] if k else []) + [(" " * left, frozenset())] + segs + pad
            out.append((paint(line, color), True))
        if r == 0:
            out.append(dim_rule())
    return out


def records(rows, color, cols):
    """A table too wide for `cols` even with narrow columns, as one record per body row: `header: value` cells (the
    header dim) packed on a line while they fit, a value too long for a line wrapped under itself, and a dim rule
    between records as wide as their widest line, as a table's rule spans the table. Squeezed further, every cell
    would wrap to a few letters a line."""
    dim = frozenset({"dim"})
    head = cells(rows[0])
    if len(rows) == 1:
        return [(paint(l, color), True) for l in pack([(inline(x, frozenset({"b"})), ()) for x in head], cols)]
    recs = []
    for row in rows[1:]:
        vals = cells(row)
        fields = []
        for k in range(max(len(head), len(vals))):
            name, val = (head[k] if k < len(head) else ""), (vals[k] if k < len(vals) else "")
            label = inline(name, dim) + [(":" + (" " if val else ""), dim)] if name else []
            lead = [(" " * (width(name) + 2), frozenset())] if name else ()
            fields.append((label + inline(val), lead))
        recs.append(pack(fields, cols))
    wide = max((width("".join(t for t, _ in l)) for rec in recs for l in rec), default=0)
    rule = (paint([("─" * min(max(wide, 1), cols), dim)], color), False)
    return [line for i, rec in enumerate(recs)
            for line in ([rule] if i else []) + [(paint(l, color), True) for l in rec]]


def pack(fields, cols):
    """(segments, lead) fields as lines: consecutive ones that each fit a line share it, joined by a dim ` · `, while
    the line fits `cols`, so a record of short cells takes a few lines, not one a cell. A field too long for a line
    gets its own, wrapped with `lead`. An empty field is left out: on a shared line it would be a stray ` · `."""
    lines, cur, w = [], [], 0
    for segs, lead in fields:
        n = width("".join(t for t, _ in segs))
        if not n:
            continue
        if cur and (n > cols or w + 3 + n > cols):
            lines.append(cur)
            cur, w = [], 0
        if n > cols:
            lines += wrap(segs, cols, lead, least=1)
        elif cur:
            cur, w = cur + [(" · ", frozenset({"dim"}))] + segs, w + 3 + n
        else:
            cur, w = list(segs), n
    return lines + ([cur] if cur else [])


def wrap(segs, cols, lead=(), least=20):
    """Segments as lines at most `cols` wide, broken between words. Lines after the first start with `lead` (spaces
    under a bullet's text, the bar of a quote), so wrapped text hangs under its first word and not at column 0.
    Under `least` columns it doesn't wrap at all (a table cell may be narrower than that)."""
    if cols < least or width("".join(t for t, _ in segs)) <= cols:
        return [segs]
    lw = width("".join(t for t, _ in lead))
    lead, lw = (list(lead), lw) if lw <= cols // 2 else ([], 0)
    lines, cur, w = [], [], 0

    def close():
        while cur and not cur[-1][0].strip():  # no line ends in the spaces it broke at
            cur.pop()
        lines.append(cur)
        return list(lead), lw
    for t, st in segs:
        for word in re.split(r"( +)", t):
            if not word or word.isspace() and (lines and w == lw or w + width(word) > cols):
                continue
            if not word.isspace() and w + width(word) > cols and w > (lw if lines else 0):
                cur, w = close()
            while w + width(word) > cols:  # one word wider than a line: cut where the line ends
                cut = fit(word, cols - w)
                cur.append((word[:cut], st))
                word = word[cut:]
                cur, w = close()
            if word:
                cur.append((word, st))
                w += width(word)
    if cur != lead:
        close()
    return lines


def fit(word, cols):
    """How many characters of `word` fit in `cols` columns (at least one)."""
    n = 0
    while n < len(word) and width(word[:n + 1]) <= cols:
        n += 1
    return max(1, n)


def blocks(text, color, cols=0):
    """Assistant text as blocks of terminal lines, each line with whether it counts towards the clip: headings,
    lists, quotes, rules, fenced code and tables by line, the rest inline, wrapped to `cols` (0: not wrapped).
    A table is one block; blank lines and rules don't count. Code blocks get a dim gutter and their language, with
    nothing inside them read as Markdown."""
    lines, out, i, fence = text.split("\n"), [], 0, None
    dim = frozenset({"dim"})

    def add(segs, lead=()):
        out.append([(paint(l, color), True) for l in wrap(segs, cols, lead)])
    while i < len(lines):
        line = lines[i].rstrip()
        m = FENCE.match(line)
        if fence:
            if m and m.group(1)[0] == fence[0] and len(m.group(1)) >= len(fence) and not m.group(2):
                fence = None
            else:
                gutter = [("  │ ", dim)]  # a long code line wraps under its gutter
                out.append([(paint(l, color), True) for l in wrap(gutter + [(line, frozenset())], cols, gutter)])
        elif m:
            fence = m.group(1)
            out.append([(paint([("  ┌ " + m.group(2) if m.group(2) else "  ┌", dim)], color), True)])
        elif "|" in line and i + 1 < len(lines) and "|" in lines[i + 1] and TABLE_RULE.match(lines[i + 1]):
            rows, rule, i = [line], lines[i + 1], i + 2
            while i < len(lines) and "|" in lines[i] and lines[i].strip():
                rows.append(lines[i])
                i += 1
            out.append(table(rows, rule, color, cols))
            continue
        elif m := HEADING.match(line):
            add(inline(m.group(1), frozenset({"b", "h"})))
        elif RULE.match(line):
            out.append([(paint([("─" * 40, dim)], color), False)])
        elif m := BULLET.match(line):
            mark = "  " + m.group(1) + "• "
            add([(mark, frozenset())] + inline(m.group(2)), [(" " * len(mark), frozenset())])
        elif m := NUMBERED.match(line):
            mark = "  " + m.group(1) + m.group(2) + " "
            add([(mark, frozenset())] + inline(m.group(3)), [(" " * len(mark), frozenset())])
        elif m := QUOTE.match(line):
            add([("▎ ", dim)] + inline(m.group(1), frozenset({"i"})), [("▎ ", dim)])
        elif not line.strip():
            out.append([("", False)])
        else:
            add(inline(line))
        i += 1
    return out


def markdown(text, color, cols=0):
    """Assistant text as terminal lines (see `blocks`)."""
    return [line for b in blocks(text, color, cols) for line, _ in b]


def clip_rendered(parts, n, color, more=4):
    """Whole blocks up to n counted lines. The block that crosses n is shown up to `more` lines past it, so a table
    that started shows rows and a wrapped paragraph its end. A clipped message ends with a reset, so no colour can
    run on."""
    shown, count, total = [], 0, sum(len(b) for b in parts)
    for b in parts:
        if count >= n and any(k for _, k in b):
            break
        for line, k in b:
            if count + k > n + more:
                break
            shown.append(line)
            count += k
    if len(shown) == total:
        return "\n".join(shown)
    while shown and not shown[-1]:
        shown.pop()
    return "\n".join(shown) + ("\033[0m" if color else "") + f"\n… (+{total - len(shown)} lines)"


def show(ev, out_lines):
    t = ev.get("type")
    if t == "assistant":
        for b in ev.get("message", {}).get("content", []):
            if b.get("type") == "text" and b.get("text", "").strip():
                print(c("1", "› ") + clip_rendered(blocks(b["text"].strip(), tty, shutil.get_terminal_size().columns - 2), TEXT_LINES, tty))
            elif b.get("type") == "tool_use":
                i = b.get("input", {})
                arg = i.get("command") or i.get("file_path") or i.get("pattern") or json.dumps(i)[:120]
                print(c("36", f"  {b['name']}: ") + clip(arg, 3))
    elif t == "user" and out_lines:
        for b in ev.get("message", {}).get("content", []) if isinstance(ev.get("message", {}).get("content"), list) else []:
            if b.get("type") == "tool_result":
                content = b.get("content")
                if isinstance(content, list):
                    content = "\n".join(x.get("text", "") for x in content if isinstance(x, dict))
                if content and str(content).strip():
                    print(c("2", "    " + clip(content, out_lines).replace("\n", "\n    ")))
    elif t == "result":
        print(c("32", f"■ done: {ev.get('num_turns')} turns, {ev.get('duration_ms', 0) / 60000:.0f} min"))


def main(p, n=30, out_lines=6):
    sys.stdout.reconfigure(line_buffering=True)  # piped into less or a file, it still streams
    runs, logf = p.state / "runs", p.state / "autopilot.log"
    current, pos, log_pos = None, 0, logf.stat().st_size if logf.exists() else 0
    first = True
    try:
        while True:
            files = sorted(runs.glob("*.jsonl"), key=lambda f: f.stat().st_mtime) if runs.exists() else []
            if files and files[-1] != current:
                current, pos = files[-1], 0
                print(c("35", f"\n══ {current.name}"))
                if first and n:
                    lines = current.read_text().splitlines()
                    for line in lines[-n:]:
                        try:
                            show(json.loads(line), out_lines)
                        except ValueError:
                            pass
                    pos = current.stat().st_size
                elif first:
                    pos = current.stat().st_size
                first = False
            if current:
                with current.open() as f:
                    f.seek(pos)
                    chunk = f.read()
                    done = chunk.rfind("\n") + 1
                    for line in chunk[:done].splitlines():
                        try:
                            show(json.loads(line), out_lines)
                        except ValueError:
                            pass
                    pos += len(chunk[:done].encode())
            if logf.exists() and logf.stat().st_size > log_pos:
                with logf.open() as f:
                    f.seek(log_pos)
                    for line in f.read().splitlines():
                        print(c("33", f"[driver] {line}"))
                    log_pos = f.tell()
            time.sleep(1)
    except KeyboardInterrupt:
        pass
